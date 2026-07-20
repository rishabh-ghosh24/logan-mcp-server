#!/bin/sh
set -eu
umask 077

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SSH_CONFIG=$(mktemp "${TMPDIR:-/tmp}/assurance-logan-admin-ssh.XXXXXX")
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    rm -f -- "$SSH_CONFIG"
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

cat > "$SSH_CONFIG" <<EOF
Host automation1
    HostName 130.162.53.112
    User opc
    Port 22
    IdentityFile "$SCRIPT_DIR/logan.key"
    IdentitiesOnly yes
    UserKnownHostsFile "$SCRIPT_DIR/internal/known_hosts"
    StrictHostKeyChecking yes
    BatchMode yes
EOF
chmod 600 "$SSH_CONFIG"

LOGAN_ADMIN_SSH_CONFIG=$SSH_CONFIG
export LOGAN_ADMIN_SSH_CONFIG
PATH="$SCRIPT_DIR/internal/bin:$PATH"
export PATH

"$SCRIPT_DIR/internal/cam-setup/admin/macos/Provision-Logan-CAM.command" \
    --ssh-target automation1 \
    --output "$HOME/logan-cam-bundles"
