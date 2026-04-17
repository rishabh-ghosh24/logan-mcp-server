"""Root conftest.py — mock the `mcp` package so tests can run without it."""

import sys
from unittest.mock import MagicMock

# Only mock if mcp is not already installed
if "mcp" not in sys.modules:
    try:
        import mcp  # noqa: F401
    except ModuleNotFoundError:
        # Build a minimal mock hierarchy that satisfies all import statements
        _mcp = MagicMock()
        _mcp_server = MagicMock()
        _mcp_server_stdio = MagicMock()
        _mcp_types = MagicMock()

        # Make submodules importable as real packages
        _mcp.server = _mcp_server
        _mcp_server.stdio = _mcp_server_stdio

        sys.modules["mcp"] = _mcp
        sys.modules["mcp.server"] = _mcp_server
        sys.modules["mcp.server.stdio"] = _mcp_server_stdio
        sys.modules["mcp.types"] = _mcp_types
