#!/usr/bin/env python3
"""Admin script to promote high-quality user queries to shared storage.

Run periodically via cron:
    0 2 * * * cd /path/to/logan-mcp-server && venv/bin/python scripts/promote_queries.py

Or manually:
    python scripts/promote_queries.py [--base-dir ~/.oci-logan-mcp]
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from oci_logan_mcp.promote import promote_all

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def main():
    parser = argparse.ArgumentParser(description="Promote user queries to shared storage")
    parser.add_argument("--base-dir", type=Path, default=Path.home() / ".oci-logan-mcp",
                        help="Base directory (default: ~/.oci-logan-mcp)")
    args = parser.parse_args()
    result = promote_all(args.base_dir)
    print(f"Done: promoted {result['promoted']} queries from {result['scanned_users']} users")

if __name__ == "__main__":
    main()
