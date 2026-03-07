"""OCI Log Analytics MCP Server.

A Model Context Protocol (MCP) server that enables natural language
interaction with Oracle Cloud Infrastructure (OCI) Log Analytics.
"""

__version__ = "1.0.0"
__author__ = "OCI Log Analytics MCP Team"

from .server import main, OCILogAnalyticsMCPServer

__all__ = ["main", "OCILogAnalyticsMCPServer", "__version__"]
