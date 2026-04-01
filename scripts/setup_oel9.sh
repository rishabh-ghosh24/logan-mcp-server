#!/bin/bash
# Setup script for OCI Log Analytics MCP Server on Oracle Enterprise Linux 9.
#
# What this script does:
#   1. Checks for Python 3.10+ (installs Python 3.11 via dnf if missing)
#   2. Creates a Python virtual environment (venv/)
#   3. Installs pip and all required dependencies
#   4. Installs the oci-logan-mcp server package
#
# Prerequisites: git (to clone the repo), OCI instance principal configured
#
# Usage:
#   chmod +x scripts/setup_oel9.sh
#   ./scripts/setup_oel9.sh

set -euo pipefail

echo "================================================"
echo " OCI Log Analytics MCP Server - OEL 9 Setup"
echo "================================================"

# Check Python version (3.10+ required)
PYTHON_CMD=""
for cmd in python3.11 python3.12 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "Python 3.10+ is required. Installing Python 3.11..."
    sudo dnf install -y python3.11 python3.11-pip
    PYTHON_CMD="python3.11"
fi

echo "Using Python: $PYTHON_CMD ($($PYTHON_CMD --version))"

# Create virtual environment
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON_CMD -m venv venv
fi

echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install the package
echo "Installing oci-logan-mcp..."
pip install -e ".[dev]"

echo ""
echo "================================================"
echo " Setup complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "  1. Run the setup wizard:  oci-logan-mcp --setup"
echo "  2. Or set environment variables:"
echo "     export OCI_LA_NAMESPACE=your-namespace"
echo "     export OCI_LA_COMPARTMENT=ocid1.compartment..."
echo "  3. Run: source venv/bin/activate && oci-logan-mcp"
echo ""
