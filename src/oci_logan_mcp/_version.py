"""Read the installed distribution version used by the package and MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("oci-logan-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
