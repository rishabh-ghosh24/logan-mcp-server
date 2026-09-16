"""Authenticated, bounded Streamable HTTP transport for Logan MCP.

The existing ``oci-logan-mcp`` command remains the full-featured stdio server.
This module exposes a deliberately smaller read-only surface for remote clients.
Imports of the MCP SDK and OCI application are deferred so policy tests do not
load credentials or initialize OCI.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import stat
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_TOOLS = frozenset(
    {
        "test_connection",
        "get_log_summary",
        "list_log_sources",
        "list_fields",
        "validate_query",
        "run_query",
    }
)
HARD_MAX_RESULTS = 100
HARD_MAX_WINDOW_MINUTES = 1_440
HARD_MAX_QUERY_ATTEMPTS = 1_000
MAX_BODY_BYTES = 65_536
MAX_RESPONSE_BYTES = 524_288
LOG = logging.getLogger("oci_logan_mcp.http")


class PolicyError(ValueError):
    """A safe, client-visible policy rejection; never include supplied values."""


@dataclass(frozen=True)
class HTTPSettings:
    """Validated deployment settings for the bounded HTTP endpoint."""

    public_url: str
    token_file: Path
    compartment_id: str
    user: str = "https.client"
    max_results: int = HARD_MAX_RESULTS
    max_window_minutes: int = HARD_MAX_WINDOW_MINUTES
    max_query_attempts: int = 100
    query_timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> HTTPSettings:
        """Load settings without accepting the bearer secret in the environment."""

        required = {
            "LOGAN_HTTP_PUBLIC_URL": os.environ.get("LOGAN_HTTP_PUBLIC_URL"),
            "LOGAN_HTTP_TOKEN_FILE": os.environ.get("LOGAN_HTTP_TOKEN_FILE"),
            "LOGAN_HTTP_COMPARTMENT_ID": os.environ.get("LOGAN_HTTP_COMPARTMENT_ID"),
        }
        if any(not value for value in required.values()):
            raise ValueError(
                "LOGAN_HTTP_PUBLIC_URL, LOGAN_HTTP_TOKEN_FILE, and "
                "LOGAN_HTTP_COMPARTMENT_ID are required."
            )
        settings = cls(
            public_url=required["LOGAN_HTTP_PUBLIC_URL"] or "",
            token_file=Path(required["LOGAN_HTTP_TOKEN_FILE"] or ""),
            compartment_id=required["LOGAN_HTTP_COMPARTMENT_ID"] or "",
            user=os.environ.get("LOGAN_HTTP_USER", "https.client"),
            max_results=_env_integer("LOGAN_HTTP_MAX_RESULTS", HARD_MAX_RESULTS),
            max_window_minutes=_env_integer(
                "LOGAN_HTTP_MAX_WINDOW_MINUTES", HARD_MAX_WINDOW_MINUTES
            ),
            max_query_attempts=_env_integer("LOGAN_HTTP_MAX_QUERY_ATTEMPTS", 100),
            query_timeout_seconds=_env_integer("LOGAN_HTTP_QUERY_TIMEOUT_SECONDS", 60),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        parsed = urlsplit(self.public_url)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path != "/mcp"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or port == -1
        ):
            raise ValueError(
                "LOGAN_HTTP_PUBLIC_URL must be an HTTPS URL ending in /mcp."
            )
        if not self.token_file.is_absolute():
            raise ValueError("LOGAN_HTTP_TOKEN_FILE must be an absolute path.")
        if not re.fullmatch(
            r"ocid1\.(?:compartment|tenancy)\.[a-z0-9-]+\.\.[A-Za-z0-9]+",
            self.compartment_id,
        ):
            raise ValueError(
                "LOGAN_HTTP_COMPARTMENT_ID must be a compartment or tenancy OCID."
            )
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.user):
            raise ValueError("LOGAN_HTTP_USER has an unsupported format.")
        _integer(self.max_results, 1, HARD_MAX_RESULTS, "LOGAN_HTTP_MAX_RESULTS")
        _integer(
            self.max_window_minutes,
            5,
            HARD_MAX_WINDOW_MINUTES,
            "LOGAN_HTTP_MAX_WINDOW_MINUTES",
        )
        _integer(
            self.max_query_attempts,
            1,
            HARD_MAX_QUERY_ATTEMPTS,
            "LOGAN_HTTP_MAX_QUERY_ATTEMPTS",
        )
        _integer(
            self.query_timeout_seconds,
            1,
            300,
            "LOGAN_HTTP_QUERY_TIMEOUT_SECONDS",
        )


def _env_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{name} must be an integer.")
    return int(raw)


def _integer(value: Any, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise PolicyError(f"{name} must be an integer from {minimum} to {maximum}.")
    return value


def _text(value: Any, maximum: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PolicyError(
            f"{name} must be nonempty text of at most {maximum} characters."
        )
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise PolicyError(f"{name} contains unsupported control characters.")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise PolicyError("Times must be RFC3339 timestamps with a timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise PolicyError("Times must be RFC3339 timestamps with a timezone.") from None
    if parsed.tzinfo is None:
        raise PolicyError("Times must include a timezone.")
    return parsed.astimezone(timezone.utc)


def normalize_arguments(
    name: str,
    arguments: Any,
    settings: HTTPSettings,
    now: datetime | None = None,
) -> dict:
    """Validate every call independently of tool-discovery JSON Schema."""
    if name not in ALLOWED_TOOLS:
        raise PolicyError("Tool is not available on this endpoint.")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise PolicyError("Tool arguments must be an object.")
    query_tools = {"run_query", "validate_query", "get_log_summary"}
    allowed = set()
    if name in query_tools:
        allowed |= {"lookback_minutes", "time_start", "time_end"}
    if name in {"run_query", "validate_query"}:
        allowed.add("query")
    if name in {"run_query", "get_log_summary", "list_fields", "list_log_sources"}:
        allowed.add("max_results")
    if name in {"list_fields", "list_log_sources"}:
        allowed.add("offset")
    if set(arguments) - allowed:
        raise PolicyError(
            "Unsupported arguments; scope and budget controls are fixed by the server."
        )
    out = {}
    if "query" in allowed:
        out["query"] = _text(arguments.get("query"), 16_000, "query")
        # The installed upstream client deliberately bypasses its result cap for
        # cluster queries. Match that exact detection and deny this first version.
        if re.search(r"\|\s*cluster\b", out["query"], re.IGNORECASE):
            raise PolicyError(
                "Cluster queries are unavailable on this bounded endpoint."
            )
    if "max_results" in allowed:
        out["max_results"] = _integer(
            arguments.get("max_results", settings.max_results),
            1,
            settings.max_results,
            "max_results",
        )
    if "offset" in allowed:
        out["offset"] = _integer(arguments.get("offset", 0), 0, 10_000, "offset")
    if name in query_tools or name == "test_connection":
        now = now or datetime.now(timezone.utc)
        explicit = "time_start" in arguments or "time_end" in arguments
        if explicit:
            if (
                "lookback_minutes" in arguments
                or not {"time_start", "time_end"} <= arguments.keys()
            ):
                raise PolicyError("Supply both times or a lookback, not both forms.")
            start, end = _timestamp(arguments["time_start"]), _timestamp(
                arguments["time_end"]
            )
        else:
            minutes = (
                5
                if name == "test_connection"
                else _integer(
                    arguments.get("lookback_minutes", 60),
                    1,
                    settings.max_window_minutes,
                    "lookback_minutes",
                )
            )
            start, end = now - timedelta(minutes=minutes), now
        if (
            not timedelta(0)
            < end - start
            <= timedelta(minutes=settings.max_window_minutes)
        ):
            raise PolicyError(
                "The query window must be positive and within the server limit."
            )
        if end > now + timedelta(seconds=30):
            raise PolicyError("The query window cannot be in the future.")
        out.update(time_start=start.isoformat(), time_end=end.isoformat())
    return out


def tool_definitions(settings: HTTPSettings) -> list[dict]:
    """Keep discovery intentionally small; no upstream private context/resources."""
    integer_limit = {
        "type": "integer",
        "minimum": 1,
        "maximum": settings.max_results,
        "default": settings.max_results,
    }
    time_properties = {
        "lookback_minutes": {
            "type": "integer",
            "minimum": 1,
            "maximum": settings.max_window_minutes,
            "default": min(60, settings.max_window_minutes),
        },
        "time_start": {
            "type": "string",
            "description": "RFC3339 start, with timezone; supply time_end too.",
        },
        "time_end": {
            "type": "string",
            "description": "RFC3339 end, with timezone; maximum window 24 hours.",
        },
    }
    descriptions = {
        "test_connection": "Test Log Analytics with a five-minute count in the server-configured scope.",
        "get_log_summary": "Count logs by source in the server-configured scope for a bounded window.",
        "list_log_sources": "List source metadata in the server-configured scope; use offset to page.",
        "list_fields": "List namespace field definitions; no log values are returned.",
        "validate_query": "Check query syntax and field names; validation is advisory, not execution.",
        "run_query": f"Read at most {settings.max_results} log rows from the server-configured scope, without subcompartments. Cluster queries are unavailable.",
    }
    definitions = []
    for name in sorted(ALLOWED_TOOLS):
        props, required = {}, []
        if name in {"get_log_summary", "validate_query", "run_query"}:
            props.update(time_properties)
        if name in {"validate_query", "run_query"}:
            props["query"] = {"type": "string", "minLength": 1, "maxLength": 16_000}
            required.append("query")
        if name in {"get_log_summary", "run_query", "list_fields", "list_log_sources"}:
            props["max_results"] = integer_limit
        if name in {"list_fields", "list_log_sources"}:
            props["offset"] = {
                "type": "integer",
                "minimum": 0,
                "maximum": 10_000,
                "default": 0,
            }
        definitions.append(
            {
                "name": name,
                "description": descriptions[name],
                "inputSchema": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "openWorldHint": True,
                },
            }
        )
    return definitions


class ToolService:
    """The only route from public tools to the existing private application."""

    def __init__(self, backend: Any, settings: HTTPSettings):
        self.backend = backend
        self.settings = settings
        # Upstream trackers/context are process-local mutable objects; serialize calls.
        self._lock = asyncio.Lock()
        self._query_attempts = 0

    async def call(self, name: str, arguments: Any) -> dict:
        args = normalize_arguments(name, arguments, self.settings)
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=1)
        except asyncio.TimeoutError:
            raise PolicyError("The endpoint is busy; retry shortly.") from None
        try:
            result = await self._dispatch(name, args)
            if (
                len(json.dumps(result, ensure_ascii=True, allow_nan=False).encode())
                > MAX_RESPONSE_BYTES
            ):
                raise PolicyError(
                    "Result is too large; narrow the query or request fewer rows."
                )
            return result
        finally:
            self._lock.release()

    async def _dispatch(self, name: str, args: dict) -> dict:
        if name in {"list_log_sources", "list_fields"}:
            if name == "list_log_sources":
                items = await self.backend.sources()
                allowed_keys = {"name", "display_name", "description", "is_system"}
            else:
                items = await self.backend.fields()
                allowed_keys = {"name", "display_name", "data_type", "description"}
            offset, limit = args["offset"], args["max_results"]
            page = [
                {key: value for key, value in item.items() if key in allowed_keys}
                for item in items[offset : offset + limit]
            ]
            return {
                "items": page,
                "total": len(items),
                "next_offset": (
                    offset + len(page) if offset + len(page) < len(items) else None
                ),
            }
        if name == "validate_query":
            return await self.backend.validate(
                args["query"], args["time_start"], args["time_end"]
            )
        query = args.get("query", "* | stats count by 'Log Source' | sort -count")
        limit = args.get("max_results", 1)
        if name == "test_connection":
            query, limit = "* | stats count", 1
        if self._query_attempts >= self.settings.max_query_attempts:
            raise PolicyError(
                "This service has reached its query-attempt quota. "
                "An administrator must review and restart it."
            )
        # Failures also consume an attempt. Never let repeated failing calls
        # bypass the quota, and never reset it on caller-controlled MCP sessions.
        self._query_attempts += 1
        result = await self.backend.query(
            query=query,
            time_start=args["time_start"],
            time_end=args["time_end"],
            max_results=limit,
            compartment_id=self.settings.compartment_id,
            include_subcompartments=False,
            use_cache=False,
            budget_override=False,
        )
        data = result.get("data", {})
        rows = data.get("rows", [])
        if not isinstance(rows, list):
            raise TypeError("Unsupported upstream result shape")
        completion = result.get("completion", {})
        percent = completion.get("percent_complete")
        partial = completion.get("are_partial_results")
        completion_status = "unknown"
        if partial is True or (type(percent) is int and percent < 100):
            completion_status = "partial"
        elif partial is False and percent == 100:
            completion_status = "complete"
        # Do not forward upstream next-step tools, learned context, or diagnostics.
        return {
            "request_status": "succeeded",
            "columns": data.get("columns", []),
            "rows": rows[:limit],
            "completion": {"status": completion_status, **completion},
            "returned_rows": min(len(rows), limit),
            "result_limit": limit,
            "truncated": len(rows) > limit,
            "limit_reached": len(rows) >= limit,
            "has_more": bool(result.get("has_more", False)),
            "first_page_only": True,
            "remaining_query_attempts": self.settings.max_query_attempts
            - self._query_attempts,
            "scope": {
                "compartment": "server-configured",
                "include_subcompartments": False,
            },
            "time_start": args["time_start"],
            "time_end": args["time_end"],
        }


class ExistingLoganBackend:
    """Reuse installed Logan components; never start its broad STDIO server."""

    def __init__(self, core: Any, settings: HTTPSettings):
        self.core = core
        self.settings = settings
        self._inflight = None

    @classmethod
    async def create(cls, settings: HTTPSettings) -> ExistingLoganBackend:
        os.environ["LOGAN_USER"] = settings.user
        os.environ["OCI_LOGAN_MCP_READ_ONLY"] = "1"
        os.environ["OCI_LA_COMPARTMENT"] = settings.compartment_id
        os.environ["OCI_LA_QUERY_LOGGING"] = "false"
        from oci_logan_mcp.server import OCILogAnalyticsMCPServer

        core = OCILogAnalyticsMCPServer()
        await core.initialize_core()
        if not core.handlers or not core.settings.read_only:
            raise RuntimeError("Read-only Log Analytics initialization failed")
        if core.oci_client.compartment_id != settings.compartment_id:
            raise RuntimeError(
                "Log Analytics compartment does not match the approved scope"
            )
        # Query/audit payloads may contain private log filters. Record only facade
        # tool names/outcomes, never the upstream query text or returned log rows.
        core.query_logger._enabled = False
        return cls(core, settings)

    async def query(self, **kwargs: Any) -> dict:
        if self._inflight is not None and not self._inflight.done():
            raise PolicyError("The previous query is still completing; retry shortly.")
        # The upstream estimator can issue one probe per query-specified source
        # before checking its budget. Bypass it and upstream retry/pagination
        # loops: the facade reserves one attempt and issues exactly one Query.
        import oci

        client = self.core.oci_client
        details = oci.log_analytics.models.QueryDetails(
            compartment_id=self.settings.compartment_id,
            compartment_id_in_subtree=False,
            query_string=kwargs["query"],
            max_total_count=kwargs["max_results"],
            sub_system="LOG",
            should_run_async=False,
            query_timeout_in_seconds=self.settings.query_timeout_seconds,
            time_filter=oci.log_analytics.models.TimeRange(
                time_start=_timestamp(kwargs["time_start"]),
                time_end=_timestamp(kwargs["time_end"]),
                time_zone="UTC",
            ),
        )
        task = asyncio.create_task(
            asyncio.to_thread(
                client._la_client.query,
                namespace_name=client.namespace,
                query_details=details,
                limit=kwargs["max_results"],
                retry_strategy=oci.retry.NoneRetryStrategy(),
            )
        )
        self._inflight = task

        def finished(completed: Any) -> None:
            # A disconnected caller does not cancel a running synchronous SDK
            # request. Keep the guard until it finishes and consume exceptions.
            if not completed.cancelled():
                completed.exception()
            if self._inflight is completed:
                self._inflight = None

        task.add_done_callback(finished)
        response = await asyncio.shield(task)
        return {
            "data": client._parse_query_response(response.data),
            "has_more": bool(response.has_next_page),
            "completion": {
                key: getattr(response.data, key, None)
                for key in (
                    "are_partial_results",
                    "partial_result_reason",
                    "percent_complete",
                    "is_content_hidden",
                    "query_execution_time_in_ms",
                )
            },
        }

    async def sources(self) -> list[dict]:
        return await self.core.oci_client.list_log_sources(
            compartment_id=self.settings.compartment_id
        )

    async def fields(self) -> list[dict]:
        return await self.core.oci_client.list_fields()

    async def validate(self, query: str, time_start: str, time_end: str) -> dict:
        result = await self.core.handlers.validator.validate(
            query=query, time_start=time_start, time_end=time_end
        )
        if not is_dataclass(result):
            raise RuntimeError("Unsupported upstream validation shape")
        values = asdict(result)
        return {
            key: values[key]
            for key in (
                "valid",
                "errors",
                "warnings",
                "suggestions",
                "estimated_cost",
                "suggested_fix",
            )
            if key in values
        }


def load_bearer_token(path: str) -> bytes:
    """Read a long random token from a private regular file, never an env value."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid not in {0, os.getuid()}
        ):
            raise ValueError(
                "Token file must be a private regular file owned by the service user or root."
            )
        value = os.read(descriptor, 1025).strip()
    finally:
        os.close(descriptor)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{43,512}", value):
        raise ValueError(
            "Token must be a generated base64url secret of 43 to 512 characters."
        )
    return value


class AuthenticatedMCP:
    """Authenticate every HTTP method before parsing bodies or invoking MCP."""

    def __init__(self, app: Any, token: bytes, public_url: str):
        parsed = urlsplit(public_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path != "/mcp"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "LOGAN_HTTP_PUBLIC_URL must be an HTTPS URL ending in /mcp."
            )
        if not re.fullmatch(rb"[A-Za-z0-9_-]{43,512}", token):
            raise ValueError("A generated bearer token is required.")
        self.app = app
        self._token_digest = hashlib.sha256(token).digest()
        self.origin = f"https://{parsed.netloc.lower()}"
        self.allowed_hosts = {parsed.netloc.lower()}
        if parsed.port in {None, 443}:
            self.allowed_hosts |= {
                parsed.hostname.lower(),
                f"{parsed.hostname.lower()}:443",
            }

    async def _reject(self, send: Any, status: int, message: str) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="oci-logan-mcp"'))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps({"error": message}).encode(),
            }
        )

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = scope.get("headers", [])
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        supplied = (
            auth[0][7:] if len(auth) == 1 and auth[0][:7].lower() == b"bearer " else b""
        )
        if len(supplied) > 512 or not hmac.compare_digest(
            hashlib.sha256(supplied).digest(), self._token_digest
        ):
            await self._reject(send, 401, "Authentication required.")
            return
        hosts = [v.decode("latin1").lower() for k, v in headers if k.lower() == b"host"]
        origins = [v.decode("latin1") for k, v in headers if k.lower() == b"origin"]
        if (
            len(hosts) != 1
            or hosts[0] not in self.allowed_hosts
            or (origins and origins != [self.origin])
        ):
            await self._reject(send, 403, "Unapproved host or origin.")
            return
        if scope.get("path") != "/mcp" or scope.get("query_string"):
            await self._reject(send, 404, "Unknown endpoint.")
            return
        if scope.get("method") not in {"POST", "GET", "DELETE"}:
            await self._reject(send, 405, "Method not allowed.")
            return
        buffered, size = [], 0
        if scope["method"] == "POST":
            while True:
                try:
                    message = await asyncio.wait_for(receive(), timeout=10)
                except TimeoutError:
                    await self._reject(send, 408, "Request body timed out.")
                    return
                if message["type"] == "http.disconnect":
                    return
                size += len(message.get("body", b""))
                if size > MAX_BODY_BYTES:
                    await self._reject(send, 413, "Request body too large.")
                    return
                buffered.append(message)
                if not message.get("more_body"):
                    break

        async def replay() -> dict:
            return buffered.pop(0) if buffered else await receive()

        await self.app(scope, replay, send)


def create_app(
    settings: HTTPSettings,
    token: bytes,
    backend_factory: Any = None,
) -> AuthenticatedMCP:
    """Build ASGI without touching OCI; initialization occurs only in lifespan."""
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import CallToolResult, TextContent, Tool
    from starlette.applications import Starlette
    from starlette.routing import Route

    settings.validate()
    server = Server("oci-logan-mcp-https")
    service = None

    @server.list_tools()
    async def list_tools() -> list:
        return [Tool(**definition) for definition in tool_definitions(settings)]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> Any:
        try:
            if service is None:
                raise RuntimeError("Service not initialized")
            result = await service.call(name, arguments)
            LOG.info("tool=%s outcome=success", name)
            return CallToolResult(
                content=[
                    TextContent(type="text", text=json.dumps(result, allow_nan=False))
                ],
                isError=False,
            )
        except PolicyError as error:
            LOG.info(
                "tool=%s outcome=denied",
                name if name in ALLOWED_TOOLS else "unavailable",
            )
            return CallToolResult(
                content=[TextContent(type="text", text=str(error))], isError=True
            )
        except Exception:  # noqa: BLE001 - return a fixed, credential-free error
            # No traceback, exception message, arguments, results, or credential logs.
            LOG.error(
                "tool=%s outcome=upstream_failure",
                name if name in ALLOWED_TOOLS else "unavailable",
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Log Analytics request failed. Check the service configuration or narrow the request.",
                    )
                ],
                isError=True,
            )

    public = urlsplit(settings.public_url)
    manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                public.netloc,
                public.hostname or "",
                f"{public.hostname}:443",
            ],
            allowed_origins=[f"https://{public.netloc}"],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: Any):
        nonlocal service
        if backend_factory is None:
            backend = await ExistingLoganBackend.create(settings)
        else:
            backend = await backend_factory()
        service = ToolService(backend, settings)
        async with manager.run():
            yield

    class MCPTransport:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await manager.handle_request(scope, receive, send)

    app = Starlette(
        routes=[
            Route("/mcp", endpoint=MCPTransport(), methods=["POST", "GET", "DELETE"])
        ],
        lifespan=lifespan,
    )
    return AuthenticatedMCP(app, token, settings.public_url)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tls-certfile")
    parser.add_argument("--tls-keyfile")
    args = parser.parse_args()
    if bool(args.tls_certfile) != bool(args.tls_keyfile):
        parser.error("Both TLS certificate and private-key paths are required.")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.tls_certfile:
        parser.error(
            "Non-loopback binding requires native TLS certificate and key paths."
        )
    if not 1 <= args.port <= 65535:
        parser.error("Port must be from 1 to 65535.")
    try:
        settings = HTTPSettings.from_env()
        token = load_bearer_token(str(settings.token_file))
        app = create_app(settings, token)
    except (KeyError, ValueError, OSError):
        parser.error(
            "Configure valid LOGAN_HTTP_PUBLIC_URL, LOGAN_HTTP_TOKEN_FILE, "
            "and LOGAN_HTTP_COMPARTMENT_ID values."
        )
    # Suppress the underlying application's broad payload/traceback logging.
    handler = logging.StreamHandler()
    handler.addFilter(lambda record: record.name == LOG.name)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    logging.getLogger().handlers[:] = [handler]
    logging.getLogger().setLevel(logging.WARNING)
    LOG.setLevel(logging.INFO)
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        access_log=False,
        log_config=None,
        proxy_headers=False,
        server_header=False,
        ssl_certfile=args.tls_certfile,
        ssl_keyfile=args.tls_keyfile,
        timeout_keep_alive=5,
        limit_concurrency=8,
    )


if __name__ == "__main__":
    main()
