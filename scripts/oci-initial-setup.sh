#!/bin/bash
# ============================================================
# OCI Golden Image Setup Script
# Oracle Linux 9 | x86_64 | Secure & minimal
# Installs: essentials, Python 3.11, OCI CLI, Docker CE, Java
# ============================================================

set -euo pipefail
echo "===== Starting OCI Golden Image Setup ====="

# ------------------------------------------------------------
# 1. SYSTEM UPDATE
# ------------------------------------------------------------
echo "[1/5] Updating system packages..."
sudo dnf update -y

# ------------------------------------------------------------
# 2. ESSENTIALS & DEV TOOLS
# ------------------------------------------------------------
echo "[2/5] Installing essential tools..."
sudo dnf install -y \
    git \
    curl \
    wget \
    vim \
    jq \
    unzip \
    zip \
    tar \
    tree \
    tmux \
    net-tools \
    bind-utils \
    lsof \
    gcc \
    gcc-c++ \
    make \
    openssl \
    openssl-devel \
    bzip2 \
    bzip2-devel \
    libffi-devel \
    zlib-devel \
    readline-devel \
    sqlite \
    sqlite-devel

# ------------------------------------------------------------
# 3. PYTHON 3.11
# NOTE: Installed ALONGSIDE system python3.9 — NOT replacing it.
# /usr/bin/python3 stays as python3.9 (dnf and system tools depend on it).
# Use 'python3.11' explicitly, or inside a venv.
# ------------------------------------------------------------
echo "[3/5] Installing Python 3.11 alongside system Python 3.9..."
sudo dnf install -y python3.11 python3.11-pip python3.11-devel

# Sanity checks
echo "  System python3 (unchanged): $(python3 --version)"
echo "  New python3.11: $(python3.11 --version)"
echo "  dnf still uses: $(head -1 /usr/bin/dnf)"

# Minimal global tools only — no app packages, those go in venv
python3.11 -m pip install --upgrade pip
python3.11 -m pip install --upgrade virtualenv setuptools wheel

# ------------------------------------------------------------
# 4. OCI CLI
# Installed via official installer — uses its own bundled Python,
# does NOT touch system python3.
# ------------------------------------------------------------
echo "[4/5] Installing OCI CLI..."

OCI_INSTALL_SCRIPT="$HOME/oci_cli_install.sh"

# Download first, then verify before running
curl -fsSL https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh \
    -o "$OCI_INSTALL_SCRIPT"

# Basic sanity check
if ! grep -q "oracle/oci-cli" "$OCI_INSTALL_SCRIPT"; then
    echo "ERROR: OCI CLI install script looks wrong. Aborting."
    exit 1
fi

bash "$OCI_INSTALL_SCRIPT" --accept-all-defaults
rm -f "$OCI_INSTALL_SCRIPT"

# Add to PATH
export PATH="$HOME/bin:$PATH"
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
echo '[[ -s "$HOME/.oci/oci_autocomplete.sh" ]] && source "$HOME/.oci/oci_autocomplete.sh"' >> ~/.bashrc

echo "  OCI CLI: $(~/bin/oci --version 2>/dev/null || echo 'installed — re-login to verify')"

# ------------------------------------------------------------
# 5. DOCKER CE (alongside Podman — NOT replacing it)
# Removes only podman-docker shim which conflicts with Docker CLI.
# podman itself is kept intact.
# ------------------------------------------------------------
echo "[5/5] Installing Docker CE alongside Podman..."

# Remove ONLY the docker shim package, not podman itself
sudo dnf remove -y podman-docker 2>/dev/null || true

# Add Docker CE repo
sudo dnf config-manager --add-repo \
    https://download.docker.com/linux/centos/docker-ce.repo

sudo dnf install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin

sudo systemctl enable docker
sudo systemctl start docker

# Add opc to docker group
# NOTE: This allows docker use without sudo.
# Equivalent to root access via 'docker run -v /:/host'.
# Acceptable for a single-user demo/presales instance.
sudo usermod -aG docker "$USER"

echo "  Docker: $(sudo docker --version)"
if command -v podman &>/dev/null; then
    echo "  Podman: $(podman --version)"
fi

# ------------------------------------------------------------
# JAVA 11 (for Management Agent and OCI tooling)
# ------------------------------------------------------------
echo "[EXTRA] Installing Java 11..."
sudo dnf install -y java-11-openjdk java-11-openjdk-devel
echo "  Java: $(java -version 2>&1 | head -1)"

# ------------------------------------------------------------
# SHELL ALIASES
# ------------------------------------------------------------
cat >> ~/.bashrc << 'EOF'

# ---- Golden Image Aliases ----
alias dps='docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'
alias dlog='docker logs -f'
alias pip311='python3.11 -m pip'
EOF

# ------------------------------------------------------------
# SUMMARY
# ------------------------------------------------------------
echo ""
echo "============================================"
echo "  Setup Complete!"
echo "============================================"
echo ""
echo "  System python3 : $(python3 --version)  [UNCHANGED - system tools safe]"
echo "  python3.11     : $(python3.11 --version)  [Use this for venvs]"
echo "  Git            : $(git --version)"
echo "  Java           : $(java -version 2>&1 | head -1)"
echo "  Docker         : $(sudo docker --version)"
if command -v podman &>/dev/null; then
    echo "  Podman         : $(podman --version)"
fi
echo "  OCI CLI        : run 'oci --version' after re-login"
echo ""
echo "NEXT STEPS:"
echo ""
echo "  1. Reboot to apply the new kernel:"
echo ""
echo "       sudo reboot"
echo ""
echo "  2. After reboot, re-login and verify:"
echo ""
echo "       docker ps        # should work without sudo"
echo "       oci --version    # confirms OCI CLI is on PATH"
echo ""
echo "  3. For Logan MCP server:"
echo ""
echo "       git clone https://github.com/rishabh-ghosh24/logan-mcp-server.git"
echo "       cd logan-mcp-server"
echo "       python3.11 -m venv venv"
echo "       source venv/bin/activate"
echo "       pip install -e ."
echo ""
echo "  4. OCI auth — set up Instance Principal (no ~/.oci/config needed):"
echo "       - Create a Dynamic Group matching this instance"
echo "       - Add IAM policy: Allow dynamic-group <name> to use log-analytics-query in tenancy"
echo ""
echo "============================================"
