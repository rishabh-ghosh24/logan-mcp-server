"""Deterministic policy, ASGI authentication, and MCP transport tests."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from oci_logan_mcp.http_server import (
    ALLOWED_TOOLS,
    MAX_BODY_BYTES,
    AuthenticatedMCP,
    HTTPSettings,
    PolicyError,
    ToolService,
    create_app,
    load_bearer_token,
    normalize_arguments,
    tool_definitions,
)

TOKEN = b"unit_test_only_ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789"
NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
COMPARTMENT_ID = "ocid1.compartment.oc1..aaaaaaaa"
SETTINGS = HTTPSettings(
    public_url="https://logan.example.test/mcp",
    token_file=Path("/tmp/logan-http-test-token"),
    compartment_id=COMPARTMENT_ID,
    max_query_attempts=3,
)


class FakeBackend:
    def __init__(self):
        self.calls = []

    async def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return {
            "data": {"columns": [{"name": "Count"}], "rows": [[i] for i in range(120)]},
            "private_context": "MUST_NOT_RETURN",
            "next_steps": ["set_namespace"],
        }

    async def sources(self):
        self.calls.append(("sources", {}))
        return [
            {
                "name": "ApprovedSource",
                "display_name": "Approved",
                "private_context": "MUST_NOT_RETURN",
            }
        ]

    async def fields(self):
        self.calls.append(("fields", {}))
        return [
            {
                "name": "Severity",
                "data_type": "STRING",
                "possible_values": ["MUST_NOT_RETURN"],
            }
        ]

    async def validate(self, query, time_start, time_end):
        self.calls.append(
            (
                "validate",
                {"query": query, "time_start": time_start, "time_end": time_end},
            )
        )
        return {"valid": True}


class PolicyTests(unittest.TestCase):
    def test_discovery_is_exact_and_closed(self):
        definitions = tool_definitions(SETTINGS)
        self.assertEqual({t["name"] for t in definitions}, ALLOWED_TOOLS)
        for tool in definitions:
            self.assertIs(tool["inputSchema"]["additionalProperties"], False)
            self.assertTrue(tool["annotations"]["readOnlyHint"])

    def test_scope_and_budget_cannot_be_overridden(self):
        for field, value in {
            "compartment_id": COMPARTMENT_ID,
            "namespace": "other",
            "scope": "tenancy",
            "include_subcompartments": True,
            "budget_override": True,
            "time_range": "last_7_days",
        }.items():
            with self.subTest(field=field), self.assertRaises(PolicyError):
                normalize_arguments(
                    "run_query", {"query": "*", field: value}, SETTINGS, NOW
                )

    def test_time_bounds_and_types(self):
        invalid = [
            {"lookback_minutes": 1441},
            {"lookback_minutes": 0},
            {"lookback_minutes": True},
            {"time_start": "2026-09-09T11:00:00Z", "time_end": "2026-09-10T12:00:00Z"},
            {"time_start": "2026-09-10T12:00:00Z", "time_end": "2026-09-10T11:00:00Z"},
            {"time_start": "2026-09-10T11:00:00", "time_end": "2026-09-10T12:00:00Z"},
            {"time_start": "2026-09-10T12:00:00Z"},
            {
                "time_start": "2026-09-10T11:00:00Z",
                "time_end": "2026-09-10T12:00:00Z",
                "lookback_minutes": 60,
            },
            {"time_start": "2026-09-10T12:00:00Z", "time_end": "2026-09-10T13:00:00Z"},
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(PolicyError):
                normalize_arguments(
                    "run_query", {"query": "*", **arguments}, SETTINGS, NOW
                )
        good = normalize_arguments(
            "run_query", {"query": "*", "lookback_minutes": 1440}, SETTINGS, NOW
        )
        self.assertEqual(good["time_start"], "2026-09-09T12:00:00+00:00")

    def test_results_and_unknown_tool_or_args(self):
        for limit in (0, 101, True, "100", 1.5):
            with self.subTest(limit=limit), self.assertRaises(PolicyError):
                normalize_arguments(
                    "run_query", {"query": "*", "max_results": limit}, SETTINGS, NOW
                )
        for name in (
            "delete_dashboard",
            "set_namespace",
            "get_current_context",
            "__proto__",
        ):
            with self.subTest(name=name), self.assertRaises(PolicyError):
                normalize_arguments(name, {}, SETTINGS, NOW)
        with self.assertRaises(PolicyError):
            normalize_arguments("test_connection", {"query": "*"}, SETTINGS, NOW)

    def test_cluster_cannot_trigger_upstream_uncapped_path(self):
        for query in (
            "* | cluster",
            "* | ClUsTeR",
            "* |\ncluster",
            "* | cluster | head 1",
        ):
            for name in ("run_query", "validate_query"):
                with (
                    self.subTest(query=query, name=name),
                    self.assertRaises(PolicyError),
                ):
                    normalize_arguments(name, {"query": query}, SETTINGS, NOW)

    def test_list_fields_has_no_unsupported_source_filter(self):
        definition = next(
            item for item in tool_definitions(SETTINGS) if item["name"] == "list_fields"
        )
        self.assertNotIn("source_name", definition["inputSchema"]["properties"])
        with self.assertRaises(PolicyError):
            normalize_arguments(
                "list_fields", {"source_name": "Anything"}, SETTINGS, NOW
            )

    def test_settings_validate_bounds_and_required_environment(self):
        SETTINGS.validate()
        invalid = [
            HTTPSettings(
                "http://logan.example/mcp", Path("/tmp/token"), COMPARTMENT_ID
            ),
            HTTPSettings(
                "https://logan.example/other", Path("/tmp/token"), COMPARTMENT_ID
            ),
            HTTPSettings("https://logan.example/mcp", Path("token"), COMPARTMENT_ID),
            HTTPSettings(
                "https://logan.example/mcp", Path("/tmp/token"), "not-an-ocid"
            ),
            HTTPSettings(
                "https://logan.example:invalid/mcp",
                Path("/tmp/token"),
                COMPARTMENT_ID,
            ),
            HTTPSettings(
                "https://logan.example/mcp",
                Path("/tmp/token"),
                COMPARTMENT_ID,
                user="invalid@example",
            ),
            HTTPSettings(
                "https://logan.example/mcp",
                Path("/tmp/token"),
                COMPARTMENT_ID,
                max_results=101,
            ),
            HTTPSettings(
                "https://logan.example/mcp",
                Path("/tmp/token"),
                COMPARTMENT_ID,
                max_window_minutes=4,
            ),
        ]
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            HTTPSettings.from_env()

        with mock.patch.dict(
            os.environ,
            {
                "LOGAN_HTTP_PUBLIC_URL": "https://logan.example.test/mcp",
                "LOGAN_HTTP_TOKEN_FILE": "/tmp/token",
                "LOGAN_HTTP_COMPARTMENT_ID": COMPARTMENT_ID,
                "LOGAN_HTTP_MAX_RESULTS": "25",
            },
            clear=True,
        ):
            loaded = HTTPSettings.from_env()
        self.assertEqual(loaded.max_results, 25)
        self.assertEqual(loaded.user, "https.client")

    def test_token_file_permissions_contents_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_bytes(TOKEN)
            path.chmod(0o600)
            self.assertEqual(load_bearer_token(str(path)), TOKEN)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                load_bearer_token(str(path))
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load_bearer_token(str(link))
            path.write_bytes(b"weak")
            with self.assertRaises(ValueError):
                load_bearer_token(str(path))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_completion_is_preserved(self):
        class PartialBackend(FakeBackend):
            async def query(self, **kwargs):
                result = await super().query(**kwargs)
                result.update(
                    has_more=True,
                    completion={
                        "are_partial_results": True,
                        "percent_complete": 40,
                        "partial_result_reason": "timeout",
                    },
                )
                return result

        result = await ToolService(PartialBackend(), SETTINGS).call(
            "run_query", {"query": "*"}
        )
        self.assertEqual(result["completion"]["status"], "partial")
        self.assertTrue(result["has_more"])
        self.assertEqual(result["completion"]["percent_complete"], 40)

    async def test_process_quota_counts_failures_and_blocks_before_backend(self):
        class FailingBackend(FakeBackend):
            async def query(self, **kwargs):
                self.calls.append(("query", kwargs))
                raise RuntimeError("fake upstream failure")

        backend = FailingBackend()
        service = ToolService(backend, SETTINGS)
        for _ in range(SETTINGS.max_query_attempts):
            with self.assertRaises(RuntimeError):
                await service.call("run_query", {"query": "*"})
        self.assertEqual(len(backend.calls), SETTINGS.max_query_attempts)
        with self.assertRaises(PolicyError):
            await service.call("run_query", {"query": "*"})
        self.assertEqual(len(backend.calls), SETTINGS.max_query_attempts)

    async def test_fixed_query_scope_and_response_cap(self):
        backend = FakeBackend()
        result = await ToolService(backend, SETTINGS).call(
            "run_query", {"query": "*", "max_results": 3}
        )
        call = backend.calls[0][1]
        self.assertEqual(call["compartment_id"], COMPARTMENT_ID)
        self.assertFalse(call["include_subcompartments"])
        self.assertFalse(call["budget_override"])
        self.assertFalse(call["use_cache"])
        self.assertEqual(len(result["rows"]), 3)
        self.assertTrue(result["truncated"])
        self.assertNotIn("MUST_NOT_RETURN", json.dumps(result))

    async def test_summary_and_connection_are_also_scoped(self):
        for name in ("get_log_summary", "test_connection"):
            backend = FakeBackend()
            await ToolService(backend, SETTINGS).call(name, {})
            self.assertEqual(len(backend.calls), 1)
            call = backend.calls[0][1]
            self.assertEqual(call["compartment_id"], COMPARTMENT_ID)
            self.assertFalse(call["include_subcompartments"])
            if name == "test_connection":
                self.assertEqual(call["max_results"], 1)
                self.assertEqual(
                    (
                        datetime.fromisoformat(call["time_end"])
                        - datetime.fromisoformat(call["time_start"])
                    ).total_seconds(),
                    300,
                )

    async def test_denied_calls_never_touch_backend(self):
        backend = FakeBackend()
        service = ToolService(backend, SETTINGS)
        for name, args in [
            ("delete_alert", {}),
            ("run_query", {"query": "*", "scope": "tenancy"}),
            ("run_query", {"query": "*", "lookback_minutes": 1441}),
        ]:
            with self.assertRaises(PolicyError):
                await service.call(name, args)
        self.assertEqual(backend.calls, [])

    async def test_discovery_metadata_does_not_return_private_context(self):
        service = ToolService(FakeBackend(), SETTINGS)
        for name in ("list_log_sources", "list_fields"):
            result = await service.call(name, {})
            self.assertNotIn("MUST_NOT_RETURN", json.dumps(result))
        with self.assertRaises(PolicyError):
            await service.call("list_fields", {"source_name": "OtherCompartmentSource"})


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def request(
        self,
        *,
        authorization=TOKEN,
        method="POST",
        extra_headers=(),
        host=b"logan.example.test",
        origin=None,
        path="/mcp",
        body=b"{}",
    ):
        entered, sent = [], []

        async def target(scope, receive, send):
            entered.append(True)
            if scope["method"] == "POST":
                await receive()
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        app = AuthenticatedMCP(target, TOKEN, "https://logan.example.test/mcp")
        headers = [(b"host", host), *extra_headers]
        if authorization is not None:
            headers.append((b"authorization", b"Bearer " + authorization))
        if origin:
            headers.append((b"origin", origin))
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "headers": headers,
                "method": method,
                "path": path,
                "query_string": b"",
            },
            receive,
            send,
        )
        return sent[0]["status"], entered, sent

    async def test_missing_and_bad_auth_all_methods(self):
        for method in ("POST", "GET", "DELETE", "OPTIONS", "PUT"):
            for token in (None, b"invalid"):
                status, entered, sent = await self.request(
                    authorization=token, method=method
                )
                self.assertEqual(status, 401)
                self.assertEqual(entered, [])
                self.assertNotIn("invalid", str(sent))

    async def test_duplicate_auth_is_rejected(self):
        status, entered, _ = await self.request(
            extra_headers=[(b"authorization", b"Bearer " + TOKEN)]
        )
        self.assertEqual((status, entered), (401, []))

    async def test_host_origin_and_body_limits(self):
        for kwargs, expected in [
            ({"host": b"attacker.example"}, 403),
            ({"origin": b"https://attacker.example"}, 403),
            ({"path": "/private"}, 404),
            ({"body": b"x" * (MAX_BODY_BYTES + 1)}, 413),
        ]:
            status, entered, _ = await self.request(**kwargs)
            self.assertEqual((status, entered), (expected, []))

    async def test_valid_auth_reaches_asgi(self):
        status, entered, _ = await self.request()
        self.assertEqual((status, entered), (204, [True]))


class ProtocolIntegrationTests(unittest.TestCase):
    def test_streamable_http_initialization_and_discovery(self):
        from starlette.testclient import TestClient

        async def backend_factory():
            return FakeBackend()

        app = create_app(SETTINGS, TOKEN, backend_factory=backend_factory)
        headers = {
            "Authorization": "Bearer " + TOKEN.decode("ascii"),
            "Accept": "application/json, text/event-stream",
        }
        with TestClient(app, base_url="https://logan.example.test") as client:
            initialized = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200
            protocol = initialized.json()["result"]["protocolVersion"]
            listed = client.post(
                "/mcp",
                headers={**headers, "MCP-Protocol-Version": protocol},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            called = client.post(
                "/mcp",
                headers={**headers, "MCP-Protocol-Version": protocol},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "test_connection", "arguments": {}},
                },
            )
            denied = client.post(
                "/mcp",
                headers={**headers, "MCP-Protocol-Version": protocol},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "delete_dashboard", "arguments": {}},
                },
            )

        assert listed.status_code == 200
        tools = listed.json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == ALLOWED_TOOLS
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        assert called.status_code == 200
        call_result = called.json()["result"]
        assert call_result["isError"] is False
        payload = json.loads(call_result["content"][0]["text"])
        assert payload["scope"] == {
            "compartment": "server-configured",
            "include_subcompartments": False,
        }
        assert denied.status_code == 200
        assert denied.json()["result"]["isError"] is True


if __name__ == "__main__":
    unittest.main(verbosity=2)
