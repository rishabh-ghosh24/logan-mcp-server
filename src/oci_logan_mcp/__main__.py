"""Entry point for python -m oci_logan_mcp."""

import argparse
import os
import sys

from .server import main as server_main
from .wizard import run_setup_wizard


def main():
    """CLI entry point with --setup flag support."""
    parser = argparse.ArgumentParser(
        prog="oci-logan-mcp",
        description="OCI Log Analytics MCP Server",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run the interactive configuration wizard and exit",
    )
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="User identity for per-user learning (default: $LOGAN_USER or $USER)",
    )
    args = parser.parse_args()

    if args.setup:
        run_setup_wizard()
        sys.exit(0)

    if args.user:
        os.environ["LOGAN_USER"] = args.user

    server_main()


if __name__ == "__main__":
    main()
