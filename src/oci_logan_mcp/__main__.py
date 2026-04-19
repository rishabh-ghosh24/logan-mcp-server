"""Entry point for python -m oci_logan_mcp."""

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn, Optional

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
    parser.add_argument(
        "--reset-secret",
        action="store_true",
        help="Reset your confirmation secret for destructive operations",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable all mutating tools (alarms, saved searches, dashboards, "
             "notifications, preference writes). Reads remain allowed.",
    )
    args = parser.parse_args()

    # Reject invalid flag combinations
    if args.setup and args.promote_and_exit:
        parser.error("--setup and --promote-and-exit cannot be used together")
    if args.promote_and_exit and args.user:
        parser.error("--user is not used in promote mode")
    if args.base_dir and not args.promote_and_exit:
        parser.error("--base-dir only applies with --promote-and-exit")
    if args.reset_secret and not args.user:
        parser.error("--reset-secret requires --user")
    if args.reset_secret and (args.setup or args.promote_and_exit):
        parser.error("--reset-secret cannot be combined with --setup or --promote-and-exit")
    if args.read_only and (args.setup or args.promote_and_exit or args.reset_secret):
        parser.error("--read-only only applies to server startup; cannot be combined with --setup, --promote-and-exit, or --reset-secret")

    if args.reset_secret:
        if args.user:
            os.environ["LOGAN_USER"] = args.user
        _reset_secret(args.user)
        sys.exit(0)

    if args.setup:
        run_setup_wizard()
        sys.exit(0)
    elif args.promote_and_exit:
        _run_promotion(args.base_dir)
    else:
        if args.user:
            os.environ["LOGAN_USER"] = args.user
        if args.read_only:
            os.environ["OCI_LOGAN_MCP_READ_ONLY"] = "1"
        server_main()


def _reset_secret(user_id: str) -> None:
    """Reset confirmation secret for a user with identity verification."""
    import getpass
    from .config import CONFIG_PATH
    from .secret_store import SecretStore
    from .audit import AuditLogger

    if not sys.stdin.isatty():
        print("Error: --reset-secret requires an interactive terminal.",
              file=sys.stderr)
        sys.exit(1)

    base_dir = CONFIG_PATH.parent
    user_dir = base_dir / "users" / user_id

    # Identity check: OS user must own the user directory
    if user_dir.exists():
        dir_owner = os.stat(user_dir).st_uid
        if dir_owner != os.getuid():
            print(f"Error: You do not own the directory for user '{user_id}'.",
                  file=sys.stderr)
            sys.exit(1)

    secret_path = user_dir / "confirmation_secret.hash"
    store = SecretStore(secret_path)

    while True:
        secret = getpass.getpass("Enter new confirmation secret: ")
        confirm = getpass.getpass("Confirm: ")
        if secret != confirm:
            print("Secrets do not match. Try again.")
            continue
        try:
            store.set_secret(secret)
            print("Secret reset successfully.")
            audit = AuditLogger(base_dir / "logs")
            audit.log(user=user_id, tool="__secret_management",
                      args={}, outcome="secret_reset")
            return
        except ValueError as e:
            print(f"Error: {e}. Try again.")


def _run_promotion(base_dir: Optional[Path]) -> NoReturn:
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
