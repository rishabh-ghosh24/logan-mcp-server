#!/bin/bash
# Update the OCI Log Analytics MCP Server to the latest version.
#
# Usage:
#   ./scripts/update.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "Updating OCI Log Analytics MCP Server..."

# Pull latest code
git pull origin "$(git rev-parse --abbrev-ref HEAD)"

# Activate virtual environment
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
else
    echo "No virtual environment found. Run scripts/setup_oel9.sh first."
    exit 1
fi

# Upgrade pip and reinstall
pip install --upgrade pip
pip install -e ".[dev]"

echo ""
echo "Update complete! Restart the MCP server to apply changes."
