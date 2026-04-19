"""MCP Server entry point for OCI Log Analytics."""

import asyncio
import json
import logging
import os
import sys
from typing import Any, Sequence

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    Resource,
    TextContent,
    ImageContent,
)

try:
    from mcp.types import ToolAnnotations
    _HAS_ANNOTATIONS = True
except ImportError:
    _HAS_ANNOTATIONS = False

from .config import load_config, config_exists, CONFIG_PATH
from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .query_logger import QueryLogger
from .context_manager import ContextManager
from .user_store import UserStore
from .preferences import PreferenceStore
from .secret_store import SecretStore
from .audit import AuditLogger
from .tools import get_tools
from .resources import get_resources
from .handlers import MCPHandlers

# Configure logging with secret redaction
class _SecretRedactFilter(logging.Filter):
    """Redact confirmation_secret values from all log output."""
    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "msg") and isinstance(record.msg, str):
            import re
            for key in ("confirmation_secret", "confirmation_secret_confirm"):
                record.msg = re.sub(
                    rf'("{key}"\s*:\s*)"[^"]*"',
                    r'\1"<REDACTED>"',
                    record.msg,
                )
                record.msg = re.sub(
                    rf"{key}=[^\s,}}]+",
                    f"{key}=<REDACTED>",
                    record.msg,
                )
        return True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
# Apply redaction filter to root logger so it covers all libraries (including mcp)
logging.getLogger().addFilter(_SecretRedactFilter())
logger = logging.getLogger(__name__)

# Timeout for background schema refresh (seconds)
SCHEMA_REFRESH_TIMEOUT = 60

# Startup schema refresh is disabled by default because refresh_schema()
# currently uses synchronous OCI SDK calls that can block the event loop before
# the MCP initialize request is answered.
ENABLE_STARTUP_SCHEMA_REFRESH = os.getenv(
    "OCI_LA_STARTUP_SCHEMA_REFRESH", "false"
).lower() in {"1", "true", "yes", "on"}


class OCILogAnalyticsMCPServer:
    """Main MCP Server for OCI Log Analytics.

    This server exposes OCI Log Analytics capabilities through the
    Model Context Protocol, enabling natural language interaction
    with log data via AI assistants.
    """

    def __init__(self):
        """Initialize the MCP server."""
        self.server = Server("oci-log-analytics")
        self.settings = None
        self.oci_client = None
        self.cache = None
        self.query_logger = None
        self.context_manager = None
        self.user_store = None
        self.preference_store = None
        self.handlers = None

        self._setup_handlers()

    def _setup_handlers(self) -> None:
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """Return list of available tools."""
            tool_defs = get_tools()
            tools = []
            for t in tool_defs:
                kwargs = {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                }
                if _HAS_ANNOTATIONS and t.get("destructive"):
                    kwargs["annotations"] = ToolAnnotations(
                        destructiveHint=True,
                        readOnlyHint=False,
                    )
                tools.append(Tool(**kwargs))
            return tools

        @self.server.list_resources()
        async def list_resources() -> list[Resource]:
            """Return list of available resources."""
            resource_defs = get_resources()
            return [
                Resource(
                    uri=r["uri"],
                    name=r["name"],
                    description=r["description"],
                    mimeType=r["mimeType"],
                )
                for r in resource_defs
            ]

        @self.server.call_tool()
        async def call_tool(
            name: str, arguments: dict[str, Any] | None
        ) -> Sequence[TextContent | ImageContent]:
            """Handle tool calls."""
            if self.handlers is None:
                return [TextContent(type="text", text="Server not initialized")]

            arguments = arguments or {}
            results = await self.handlers.handle_tool_call(name, arguments)

            return [
                ImageContent(
                    type="image",
                    data=r["data"],
                    mimeType=r.get("mimeType", "image/png"),
                )
                if r["type"] == "image"
                else TextContent(type="text", text=r["text"])
                for r in results
            ]

        @self.server.read_resource()
        async def read_resource(uri: str) -> str:
            """Handle resource reads."""
            if self.handlers is None:
                return json.dumps({"error": "Server not initialized"})

            try:
                content = await self.handlers.handle_resource_read(uri)
                if isinstance(content, str):
                    return content
                return json.dumps(content, indent=2, default=str)
            except Exception as e:
                logger.exception(f"Error reading resource {uri}")
                return json.dumps({"error": str(e)})

    async def initialize_core(self) -> None:
        """Initialize core server components without schema refresh.

        Loads configuration, sets up OCI client, and initializes
        service components. Schema refresh is deferred to a background
        task to avoid blocking the MCP handshake.
        """
        logger.info("Initializing OCI Log Analytics MCP Server...")

        # Fail fast if no config — don't launch interactive wizard during stdio mode
        if not config_exists():
            raise RuntimeError(
                "No configuration found. Run 'oci-logan-mcp --setup' to configure "
                "the server before using it with an MCP client."
            )

        self.settings = load_config()

        # Initialize components
        self.cache = CacheManager(self.settings.cache)
        self.query_logger = QueryLogger(self.settings.logging)

        try:
            self.oci_client = OCILogAnalyticsClient(self.settings)
            logger.info(
                f"Connected to OCI Log Analytics (namespace: {self.oci_client.namespace})"
            )
        except Exception as e:
            logger.error(f"Failed to initialize OCI client: {e}")
            logger.warning("Server will start but OCI operations will fail")
            self.oci_client = None

        # Initialize context manager
        self.context_manager = ContextManager(self.settings)

        # Initialize per-user stores
        base_dir = CONFIG_PATH.parent  # ~/.oci-logan-mcp
        self.user_store = UserStore(base_dir=base_dir)
        self.preference_store = PreferenceStore(
            user_dir=base_dir / "users" / self.user_store.user_id
        )
        logger.info(f"User identity: {self.user_store.user_id}")

        # Initialize per-user secret store
        secret_path = base_dir / "users" / self.user_store.user_id / "confirmation_secret.hash"
        self.secret_store = SecretStore(secret_path)

        # Initialize shared audit logger
        self.audit_logger = AuditLogger(log_dir=base_dir / "logs")

        # Deprecation warning for old env var
        if os.environ.get("OCI_LA_CONFIRMATION_SECRET"):
            logger.warning(
                "OCI_LA_CONFIRMATION_SECRET is no longer used. "
                "Per-user secrets are now stored in the user directory."
            )

        # Interactive CLI users can still set a secret at startup, but MCP/SSH
        # sessions should continue to start even when no secret exists yet.
        if self.secret_store.has_secret() and not self.secret_store.is_valid():
            logger.warning(
                "Confirmation secret file for user '%s' is invalid. "
                "Guarded operations will remain unavailable until the secret is "
                "recreated with setup_confirmation_secret or --reset-secret.",
                self.user_store.user_id,
            )
        elif not self.secret_store.has_secret():
            import sys as _sys
            if _sys.stdin.isatty():
                import getpass
                print(f"\nNo confirmation secret found for user '{self.user_store.user_id}'.")
                print("Destructive operations (delete/update) require a secret for safety.")
                while True:
                    secret = getpass.getpass("Enter your confirmation secret: ")
                    confirm = getpass.getpass("Confirm: ")
                    if secret != confirm:
                        print("Secrets do not match. Try again.")
                        continue
                    try:
                        self.secret_store.set_secret(secret)
                        print("Secret saved. You'll need this to confirm destructive operations.\n")
                        self.audit_logger.log(
                            user=self.user_store.user_id,
                            tool="__secret_management",
                            args={}, outcome="secret_set",
                        )
                        break
                    except ValueError as e:
                        print(f"Error: {e}. Try again.")
            else:
                logger.info(
                    "No confirmation secret set for user '%s'. "
                    "Non-guarded tools will work immediately. Use the "
                    "setup_confirmation_secret tool or --reset-secret before "
                    "running destructive operations.",
                    self.user_store.user_id,
                )

        # Initialize handlers
        if self.oci_client:
            self.handlers = MCPHandlers(
                settings=self.settings,
                oci_client=self.oci_client,
                cache=self.cache,
                query_logger=self.query_logger,
                context_manager=self.context_manager,
                user_store=self.user_store,
                preference_store=self.preference_store,
                secret_store=self.secret_store,
                audit_logger=self.audit_logger,
            )

        logger.info("OCI Log Analytics MCP Server initialized")

    async def _refresh_schema_background(self) -> None:
        """Refresh schema data in background after MCP transport is live.

        Wraps refresh_schema() with a timeout to prevent indefinite stalls.
        Failures are logged but do not affect server operation — tools
        fetch schema on demand via SchemaManager.
        """
        try:
            counts = await asyncio.wait_for(
                self.context_manager.refresh_schema(
                    self.oci_client, self.settings
                ),
                timeout=SCHEMA_REFRESH_TIMEOUT,
            )
            logger.info(f"Background schema refresh complete: {counts}")
        except asyncio.TimeoutError:
            logger.warning(
                f"Background schema refresh timed out after {SCHEMA_REFRESH_TIMEOUT}s "
                "— tools will fetch on demand"
            )
        except Exception as e:
            logger.warning(
                f"Background schema refresh failed: {e} — tools will fetch on demand"
            )

    async def run(self) -> None:
        """Run the MCP server.

        Starts the server and handles stdio communication
        with MCP clients. Schema refresh runs in background after
        the transport is live. A keepalive task prevents idle-timeout
        disconnections from the client.
        """
        await self.initialize_core()

        logger.info("Starting MCP server on stdio...")
        async with stdio_server() as (read_stream, write_stream):
            schema_task = None
            if self.oci_client and ENABLE_STARTUP_SCHEMA_REFRESH and not (self.settings and self.settings.read_only):
                schema_task = asyncio.create_task(self._refresh_schema_background())
            elif self.oci_client:
                logger.info(
                    "Skipping startup schema refresh to keep MCP initialization responsive"
                )
            keepalive_task = asyncio.create_task(self._keepalive_loop())
            try:
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                )
            finally:
                if schema_task is not None:
                    schema_task.cancel()
                    try:
                        await schema_task
                    except asyncio.CancelledError:
                        pass
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass

    async def _keepalive_loop(self, interval: int = 30) -> None:
        """Periodic keepalive to prevent idle-timeout disconnections.

        Keeps the asyncio event loop active so the STDIO transport
        does not appear idle to the client process manager.
        """
        while True:
            await asyncio.sleep(interval)
            logger.debug("keepalive ping")


def main() -> None:
    """Entry point for the MCP server."""
    try:
        server = OCILogAnalyticsMCPServer()
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.info("Server shutdown requested")
        sys.exit(0)
    except (EOFError, BrokenPipeError, ConnectionError):
        # Client disconnected — clean exit so process managers
        # can distinguish a disconnect (code 0) from a crash (code 1).
        logger.info("Client disconnected")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
