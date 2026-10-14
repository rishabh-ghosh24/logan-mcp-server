"""Offline tests for the HTTPS endpoint verifier."""

import json
from pathlib import Path

from scripts.verify_http_endpoint import verify

TOKEN = b"unit_test_only_ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
TOOLS = {
    "test_connection",
    "get_log_summary",
    "list_log_sources",
    "list_fields",
    "validate_query",
    "run_query",
}


def test_verify_checks_auth_discovery_denials_and_query(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_bytes(TOKEN)
    token_file.chmod(0o600)

    def send(method, body=None, token=None, protocol=None):
        if method == "GET":
            return 401, None
        if body.get("method") == "notifications/initialized":
            return 202, None
        request_id = body["id"]
        rpc_method = body["method"]
        if rpc_method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
            }
        elif rpc_method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "annotations": {"readOnlyHint": True}}
                    for name in TOOLS
                ]
            }
        elif body["params"]["name"] == "test_connection":
            result = {
                "isError": False,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "scope": {
                                    "compartment": "server-configured",
                                    "include_subcompartments": False,
                                },
                                "completion": {"status": "complete"},
                                "has_more": False,
                                "time_start": "2026-09-15T12:00:00+00:00",
                                "time_end": "2026-09-15T12:05:00+00:00",
                                "rows": [[10]],
                            }
                        ),
                    }
                ],
            }
        else:
            result = {"isError": True, "content": []}
        return 200, {"jsonrpc": "2.0", "id": request_id, "result": result}

    report = verify(
        "https://logan.example.test/mcp",
        token_file,
        send=send,
        certificate_reader=lambda _url: {
            "certificate_verified": True,
            "expires_utc": "2026-12-01T00:00:00+00:00",
        },
    )

    assert report["passed"] is True
    assert [check["name"] for check in report["checks"]] == [
        "tls",
        "unauthenticated_get",
        "wrong_bearer",
        "initialize",
        "tool_discovery",
        "forbidden_tool_calls",
        "scoped_aggregate",
    ]
    assert TOKEN.decode("ascii") not in json.dumps(report)


def test_verify_reports_stage_without_response_details(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_bytes(TOKEN)
    token_file.chmod(0o600)

    report = verify(
        "https://logan.example.test/mcp",
        token_file,
        send=lambda *args, **kwargs: (200, {"sensitive": "must-not-return"}),
        certificate_reader=lambda _url: {"certificate_verified": True},
    )

    assert report["passed"] is False
    assert report["failure"]["stage"] == "unauthenticated_get"
    assert "must-not-return" not in json.dumps(report)
