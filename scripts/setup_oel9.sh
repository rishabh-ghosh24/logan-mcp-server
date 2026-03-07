#!/bin/bash
# Setup script for OCI Log Analytics MCP Server on Oracle Enterprise Linux 9.
# Run this on a fresh OEL 9 VM with OCI instance principal configured.
#
# Usage:
#   chmod +x scripts/setup_oel9.sh
#   ./scripts/setup_oel9.sh

set -euo pipefail

echo "================================================"
echo " OCI Log Analytics MCP Server - OEL 9 Setup"
echo "================================================"

# Check Python version
PYTHON_CMD=""
for cmd in python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" --version 2>&1 | awk '{print $2}')
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "Python 3.9+ is required. Installing..."
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
echo "  1. Run the setup wizard:  oci-logan-mcp"
echo "  2. Or set environment variables:"
echo "     export OCI_LA_NAMESPACE=your-namespace"
echo "     export OCI_LA_COMPARTMENT=ocid1.compartment..."
echo "  3. Run: source venv/bin/activate && oci-logan-mcp"
echo ""
