"""OCI Log Analytics MCP Server.

A Model Context Protocol (MCP) server that enables natural language
interaction with Oracle Cloud Infrastructure (OCI) Log Analytics.
"""

from ._version import __version__
__author__ = "OCI Log Analytics MCP Team"

from .server import main, OCILogAnalyticsMCPServer

__all__ = ["main", "OCILogAnalyticsMCPServer", "__version__"]
