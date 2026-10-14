#!/usr/bin/env python3
"""Verify TLS, authentication, discovery, policy, and a bounded Logan query."""

from __future__ import annotations

import argparse
import json
import math
import re
import socket
import ssl
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from oci_logan_mcp.http_server import ALLOWED_TOOLS, load_bearer_token

MAX_RESPONSE_BYTES = 1_048_576


class CheckFailed(Exception):
    """A fixed, credential-free verification error."""


class NoRedirect(HTTPRedirectHandler):
    """Prevent forwarding Authorization to another destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(
    endpoint: str,
    method: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    protocol: str | None = None,
) -> tuple[int, dict[str, Any] | None]:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if protocol:
        headers["MCP-Protocol-Version"] = protocol
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    opener = build_opener(
        HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
    )
    try:
        response = opener.open(
            Request(endpoint, data=data, headers=headers, method=method), timeout=75
        )
    except HTTPError as error:
        response = error
    with response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
        if len(content) > MAX_RESPONSE_BYTES:
            raise CheckFailed("Endpoint response exceeded the verification limit.")
        payload = None
        if content and response.status == 200:
            try:
                payload = json.loads(content)
            except (ValueError, UnicodeError):
                raise CheckFailed("Endpoint did not return MCP JSON.") from None
        return response.status, payload


def _tls_info(endpoint: str) -> dict[str, Any]:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path != "/mcp":
        raise CheckFailed("Endpoint must be an HTTPS URL ending in /mcp.")
    port = parsed.port or 443
    context = ssl.create_default_context()
    with (
        socket.create_connection((parsed.hostname, port), timeout=15) as connection,
        context.wrap_socket(connection, server_hostname=parsed.hostname) as secure,
    ):
        certificate = secure.getpeercert()
        expires = certificate.get("notAfter")
    return {
        "certificate_verified": True,
        "expires_utc": (
            datetime.fromtimestamp(
                ssl.cert_time_to_seconds(expires), timezone.utc
            ).isoformat()
            if expires
            else None
        ),
    }


def verify(
    endpoint: str,
    token_file: Path,
    send: Callable[..., tuple[int, dict[str, Any] | None]] | None = None,
    certificate_reader: Callable[[str], dict[str, Any]] = _tls_info,
) -> dict[str, Any]:
    """Return a credential-free verification report."""

    report: dict[str, Any] = {"endpoint": endpoint, "passed": False, "checks": []}
    stage = "tls"
    try:
        report["checks"].append(
            {"name": "tls", "status": "passed", **certificate_reader(endpoint)}
        )
        sender = send or (lambda *args, **kwargs: _request(endpoint, *args, **kwargs))
        for label, supplied in (
            ("unauthenticated_get", None),
            ("wrong_bearer", "invalid-verification-token"),
        ):
            stage = label
            status, _ = sender("GET", token=supplied)
            if status != 401:
                raise CheckFailed("Expected HTTP 401 authentication rejection.")
            report["checks"].append(
                {"name": label, "status": "passed", "http_status": status}
            )

        stage = "credential_read"
        token = load_bearer_token(str(token_file)).decode("ascii")
        protocol = None
        next_id = 0

        def rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal next_id
            next_id += 1
            status, response = sender(
                "POST",
                body={
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": method,
                    "params": params or {},
                },
                token=token,
                protocol=protocol,
            )
            if (
                status != 200
                or not isinstance(response, dict)
                or response.get("id") != next_id
            ):
                raise CheckFailed(
                    "Expected a matching MCP JSON response with HTTP 200."
                )
            return response

        stage = "initialize"
        initialized = rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "logan-endpoint-verifier", "version": "1"},
            },
        )
        initialization = initialized.get("result", {})
        protocol = initialization.get("protocolVersion")
        if not isinstance(protocol, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", protocol
        ):
            raise CheckFailed("The server did not negotiate an MCP protocol version.")
        capabilities = initialization.get("capabilities", {})
        if (
            "tools" not in capabilities
            or {
                "resources",
                "prompts",
                "logging",
            }
            & capabilities.keys()
        ):
            raise CheckFailed("The endpoint advertised unexpected capabilities.")
        status, _ = sender(
            "POST",
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=token,
            protocol=protocol,
        )
        if status not in (202, 204):
            raise CheckFailed("The server did not accept the initialized notification.")
        report["checks"].append(
            {"name": "initialize", "status": "passed", "protocol": protocol}
        )

        stage = "tool_discovery"
        listed = rpc("tools/list").get("result", {})
        definitions = listed.get("tools", [])
        if (
            {tool.get("name") for tool in definitions} != ALLOWED_TOOLS
            or listed.get("nextCursor")
            or not all(
                tool.get("annotations", {}).get("readOnlyHint") is True
                for tool in definitions
            )
        ):
            raise CheckFailed("Tool discovery did not match the read-only allowlist.")
        report["checks"].append(
            {
                "name": "tool_discovery",
                "status": "passed",
                "tool_count": len(ALLOWED_TOOLS),
            }
        )

        stage = "forbidden_tool_calls"
        for name in ("__logan_verification_unknown_tool__", "delete_dashboard"):
            response = rpc("tools/call", {"name": name, "arguments": {}})
            if not (
                "error" in response or response.get("result", {}).get("isError") is True
            ):
                raise CheckFailed("A forbidden tool call was not rejected.")
        report["checks"].append(
            {"name": "forbidden_tool_calls", "status": "passed", "denied_calls": 2}
        )

        stage = "scoped_aggregate"
        response = rpc("tools/call", {"name": "test_connection", "arguments": {}})
        result = response.get("result", {})
        if "error" in response or result.get("isError"):
            raise CheckFailed("The scoped aggregate query failed.")
        content = result.get("content", [])
        if len(content) != 1 or content[0].get("type") != "text":
            raise CheckFailed("The query returned an unexpected result structure.")
        payload = json.loads(content[0]["text"])
        if payload.get("scope") != {
            "compartment": "server-configured",
            "include_subcompartments": False,
        }:
            raise CheckFailed("The query did not use the server-configured scope.")
        if payload.get("completion", {}).get("status") != "complete" or payload.get(
            "has_more"
        ):
            raise CheckFailed("The aggregate result was partial or incomplete.")
        start, end = (
            datetime.fromisoformat(payload[key].replace("Z", "+00:00"))
            for key in ("time_start", "time_end")
        )
        rows = payload.get("rows", [])
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or (end - start).total_seconds() != 300
            or len(rows) != 1
            or len(rows[0]) != 1
            or type(rows[0][0]) not in (int, float)
            or not math.isfinite(rows[0][0])
            or rows[0][0] < 0
        ):
            raise CheckFailed(
                "The query did not return the expected bounded aggregate."
            )
        report["checks"].append(
            {"name": "scoped_aggregate", "status": "passed", "window_minutes": 5}
        )
        report["passed"] = True
    except CheckFailed as error:
        report["failure"] = {"stage": stage, "reason": str(error)}
    except Exception:  # noqa: BLE001 - never expose transport or credential details
        report["failure"] = {
            "stage": stage,
            "reason": "Verification could not complete; inspect this stage without exposing credentials.",
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="Public HTTPS /mcp URL")
    parser.add_argument("--token-file", required=True, type=Path)
    args = parser.parse_args()
    output = verify(args.url, args.token_file)
    print(json.dumps(output, indent=2, allow_nan=False))
    sys.exit(0 if output["passed"] else 1)


if __name__ == "__main__":
    main()
