"""Entry point for python -m oci_logan_mcp."""

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

from .server import main as server_main
from .wizard import run_setup_wizard
from .config import CONFIG_PATH
from .promote import promote_all


def main():
    """CLI entry point with --setup, --promote-and-exit flag support."""
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
        "--promote-and-exit",
        action="store_true",
        help="Run query promotion once and exit (admin task, not per-user)",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Base directory for promotion (default: ~/.oci-logan-mcp)",
    )
    parser.add_argument(
        "--user",
        type=str,
        default=None,
        help="User identity for per-user learning (default: $LOGAN_USER or $USER)",
    )
    args = parser.parse_args()

    # Reject invalid flag combinations
    if args.setup and args.promote_and_exit:
        parser.error("--setup and --promote-and-exit cannot be used together")
    if args.promote_and_exit and args.user:
        parser.error("--user is not used in promote mode")
    if args.base_dir and not args.promote_and_exit:
        parser.error("--base-dir only applies with --promote-and-exit")

    if args.setup:
        run_setup_wizard()
        sys.exit(0)
    elif args.promote_and_exit:
        _run_promotion(args.base_dir)
    else:
        if args.user:
            os.environ["LOGAN_USER"] = args.user
        server_main()


def _run_promotion(base_dir: Path | None) -> NoReturn:
    """Run promote_all once and exit."""
    resolved = base_dir or CONFIG_PATH.parent
    try:
        result = promote_all(resolved)
        print(
            f"Promoted {result['promoted']} queries "
            f"from {result['scanned_users']} users "
            f"(base: {resolved})"
        )
        sys.exit(0)
    except Exception as e:
        print(f"Promotion failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
