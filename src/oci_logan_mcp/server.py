"""MCP Server entry point for OCI Log Analytics."""

import asyncio
import json
import logging
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

from .config import load_config, config_exists
from .wizard import run_setup_wizard
from .client import OCILogAnalyticsClient
from .cache import CacheManager
from .query_logger import QueryLogger
from .context_manager import ContextManager
from .tools import get_tools
from .resources import get_resources
from .handlers import MCPHandlers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
        self.handlers = None

        self._setup_handlers()

    def _setup_handlers(self) -> None:
        """Register MCP protocol handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """Return list of available tools."""
            tool_defs = get_tools()
            return [
                Tool(
                    name=t["name"],
                    description=t["description"],
                    inputSchema=t["inputSchema"],
                )
                for t in tool_defs
            ]

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

    async def initialize(self) -> None:
        """Initialize server components.

        Loads configuration, sets up OCI client, and initializes
        all service components.
        """
        logger.info("Initializing OCI Log Analytics MCP Server...")

        # Check for config, run wizard if needed
        if not config_exists():
            logger.info("No configuration found, running setup wizard...")
            try:
                self.settings = run_setup_wizard()
            except (EOFError, KeyboardInterrupt):
                logger.warning("Setup wizard cancelled. Using defaults.")
                self.settings = load_config()
        else:
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

        # Initialize handlers
        if self.oci_client:
            self.handlers = MCPHandlers(
                settings=self.settings,
                oci_client=self.oci_client,
                cache=self.cache,
                query_logger=self.query_logger,
                context_manager=self.context_manager,
            )

            # Refresh schema data from OCI at startup (always fresh)
            try:
                counts = await self.context_manager.refresh_schema(
                    self.oci_client, self.settings
                )
                logger.info(f"Schema refresh at startup: {counts}")
            except Exception as e:
                logger.warning(f"Schema refresh failed at startup: {e}")

        logger.info("OCI Log Analytics MCP Server initialized")

    async def run(self) -> None:
        """Run the MCP server.

        Starts the server and handles stdio communication
        with MCP clients.  A background keepalive task prevents
        idle-timeout disconnections from the client.
        """
        await self.initialize()

        logger.info("Starting MCP server on stdio...")
        async with stdio_server() as (read_stream, write_stream):
            keepalive_task = asyncio.create_task(self._keepalive_loop())
            try:
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                )
            finally:
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
