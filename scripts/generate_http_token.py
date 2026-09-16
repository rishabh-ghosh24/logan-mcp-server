#!/usr/bin/env python3
"""Create a private bearer-token file for the Logan HTTPS MCP endpoint."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path


def write_token(path: Path) -> None:
    """Create a mode-0600 token file without following or replacing a path."""

    if not path.is_absolute():
        raise ValueError("Token path must be absolute.")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        token = secrets.token_urlsafe(48).encode("ascii")
        os.write(descriptor, token + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Absolute output path")
    args = parser.parse_args()
    try:
        write_token(args.path)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Created private bearer token file: {args.path}")


if __name__ == "__main__":
    main()
