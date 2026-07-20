#!/bin/sh
set -eu
umask 077

usage() {
    printf 'Usage: %s --key PRIVATE_KEY --output DIRECTORY [--force]\n' "$0" >&2
    exit 64
}

KEY=''
OUTPUT=''
FORCE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --key) [ "$#" -ge 2 ] || usage; KEY=$2; shift 2 ;;
        --output) [ "$#" -ge 2 ] || usage; OUTPUT=$2; shift 2 ;;
        --force) FORCE=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$KEY" ] && [ -n "$OUTPUT" ] || usage

if [ ! -f "$KEY" ] || [ -L "$KEY" ]; then
    printf 'Private key must be a regular, non-symlink file: %s\n' "$KEY" >&2
    exit 66
fi
for tool in cp chmod mkdir mktemp mv rm sha256sum zip; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        if [ "$tool" = sha256sum ] && command -v shasum >/dev/null 2>&1; then
            continue
        fi
        printf 'Required packaging tool is unavailable: %s\n' "$tool" >&2
        exit 69
    fi
done

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
HANDOVER="$ROOT/cam-setup/handover"
mkdir -p "$OUTPUT"
OUTPUT=$(CDPATH= cd -- "$OUTPUT" && pwd -P)
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/assurance-logan-handover.XXXXXX")
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    rm -rf -- "$STAGE"
    exit "$status"
}
trap cleanup EXIT HUP INT TERM

HOST_KEY='130.162.53.112 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFcj0yHMayP5k838JNY37ZUoyrv79CYtnkBf0BvXsqz1'

copy_key() {
    destination=$1
    cp "$KEY" "$destination/logan.key"
    chmod 600 "$destination/logan.key"
}

new_package() {
    name=$1
    path="$STAGE/$name"
    mkdir -p "$path"
    printf '%s\n' "$path"
}

MAC_USER=$(new_package assurance-logan-user-macos)
cp "$ROOT/macos-setup/Install-Logan-MCP.command" "$MAC_USER/Install-Logan-MCP.command"
cp "$HANDOVER/README-user.html" "$MAC_USER/README.html"
copy_key "$MAC_USER"
chmod 755 "$MAC_USER/Install-Logan-MCP.command"

WIN_USER=$(new_package assurance-logan-user-windows)
cp "$ROOT/windows-setup/Double-Click-to-Install.cmd" "$WIN_USER/Double-Click-to-Install.cmd"
cp "$ROOT/windows-setup/logan-mcp.ps1" "$WIN_USER/logan-mcp.ps1"
cp "$HANDOVER/README-user.html" "$WIN_USER/README.html"
copy_key "$WIN_USER"

MAC_ADMIN=$(new_package assurance-logan-admin-macos)
mkdir -p "$MAC_ADMIN/internal/bin" "$MAC_ADMIN/internal/cam-setup/admin/macos" "$MAC_ADMIN/internal/cam-setup/bundle/macos" "$MAC_ADMIN/internal/cam-setup/bundle/windows"
cp "$ROOT/macos-setup/Install-Logan-MCP.command" "$MAC_ADMIN/1-Install-My-Assurance-Profile.command"
cp "$HANDOVER/macos/2-Provision-CAM.command" "$MAC_ADMIN/2-Provision-CAM.command"
cp "$HANDOVER/macos/3-Revoke-CAM.command" "$MAC_ADMIN/3-Revoke-CAM.command"
cp "$HANDOVER/macos/ssh" "$MAC_ADMIN/internal/bin/ssh"
cp "$ROOT/cam-setup/admin/macos/Provision-Logan-CAM.command" "$MAC_ADMIN/internal/cam-setup/admin/macos/Provision-Logan-CAM.command"
cp "$ROOT/cam-setup/admin/macos/Deprovision-Logan-CAM.command" "$MAC_ADMIN/internal/cam-setup/admin/macos/Deprovision-Logan-CAM.command"
cp "$ROOT/cam-setup/bundle/macos/Install-Logan-MCP.command" "$MAC_ADMIN/internal/cam-setup/bundle/macos/Install-Logan-MCP.command"
cp "$ROOT/cam-setup/bundle/windows/Double-Click-to-Install.cmd" "$MAC_ADMIN/internal/cam-setup/bundle/windows/Double-Click-to-Install.cmd"
cp "$ROOT/cam-setup/bundle/windows/Install-Logan-MCP.ps1" "$MAC_ADMIN/internal/cam-setup/bundle/windows/Install-Logan-MCP.ps1"
cp "$ROOT/cam-setup/bundle/README.html" "$MAC_ADMIN/internal/cam-setup/bundle/README.html"
cp "$HANDOVER/README-admin.html" "$MAC_ADMIN/README.html"
printf '%s\n' "$HOST_KEY" > "$MAC_ADMIN/internal/known_hosts"
copy_key "$MAC_ADMIN"
chmod 755 "$MAC_ADMIN"/*.command "$MAC_ADMIN/internal/cam-setup/admin/macos"/*.command "$MAC_ADMIN/internal/bin/ssh"
chmod 600 "$MAC_ADMIN/internal/known_hosts"

WIN_ADMIN=$(new_package assurance-logan-admin-windows)
mkdir -p "$WIN_ADMIN/internal/cam-setup/admin/windows" "$WIN_ADMIN/internal/cam-setup/bundle/macos" "$WIN_ADMIN/internal/cam-setup/bundle/windows"
cp "$ROOT/windows-setup/Double-Click-to-Install.cmd" "$WIN_ADMIN/1-Install-My-Assurance-Profile.cmd"
cp "$ROOT/windows-setup/logan-mcp.ps1" "$WIN_ADMIN/logan-mcp.ps1"
cp "$HANDOVER/windows/2-Provision-CAM.cmd" "$WIN_ADMIN/2-Provision-CAM.cmd"
cp "$HANDOVER/windows/2-Provision-CAM.ps1" "$WIN_ADMIN/2-Provision-CAM.ps1"
cp "$HANDOVER/windows/3-Revoke-CAM.cmd" "$WIN_ADMIN/3-Revoke-CAM.cmd"
cp "$HANDOVER/windows/3-Revoke-CAM.ps1" "$WIN_ADMIN/3-Revoke-CAM.ps1"
cp "$ROOT/cam-setup/admin/windows/Provision-Logan-CAM.ps1" "$WIN_ADMIN/internal/cam-setup/admin/windows/Provision-Logan-CAM.ps1"
cp "$ROOT/cam-setup/admin/windows/Deprovision-Logan-CAM.ps1" "$WIN_ADMIN/internal/cam-setup/admin/windows/Deprovision-Logan-CAM.ps1"
cp "$ROOT/cam-setup/bundle/macos/Install-Logan-MCP.command" "$WIN_ADMIN/internal/cam-setup/bundle/macos/Install-Logan-MCP.command"
cp "$ROOT/cam-setup/bundle/windows/Double-Click-to-Install.cmd" "$WIN_ADMIN/internal/cam-setup/bundle/windows/Double-Click-to-Install.cmd"
cp "$ROOT/cam-setup/bundle/windows/Install-Logan-MCP.ps1" "$WIN_ADMIN/internal/cam-setup/bundle/windows/Install-Logan-MCP.ps1"
cp "$ROOT/cam-setup/bundle/README.html" "$WIN_ADMIN/internal/cam-setup/bundle/README.html"
cp "$HANDOVER/README-admin.html" "$WIN_ADMIN/README.html"
printf '%s\n' "$HOST_KEY" > "$WIN_ADMIN/internal/known_hosts"
copy_key "$WIN_ADMIN"
chmod 600 "$WIN_ADMIN/internal/known_hosts"

for package in assurance-logan-user-macos assurance-logan-user-windows assurance-logan-admin-macos assurance-logan-admin-windows; do
    destination="$OUTPUT/$package"
    archive="$OUTPUT/$package.zip"
    if [ "$FORCE" -ne 1 ] && { [ -e "$destination" ] || [ -e "$archive" ]; }; then
        printf 'Refusing to overwrite existing package: %s\n' "$package" >&2
        exit 73
    fi
    rm -rf -- "$destination"
    rm -f -- "$archive"
    (cd "$STAGE" && zip -q -r -X "$archive" "$package")
    chmod 600 "$archive"
done

CHECKSUMS="$OUTPUT/SHA256SUMS.txt"
rm -f -- "$CHECKSUMS"
if command -v sha256sum >/dev/null 2>&1; then
    (cd "$OUTPUT" && sha256sum ./*.zip) > "$CHECKSUMS"
else
    (cd "$OUTPUT" && shasum -a 256 ./*.zip) > "$CHECKSUMS"
fi
chmod 600 "$CHECKSUMS"

printf 'Created four handover ZIPs in %s\n' "$OUTPUT"
printf 'CAM recipient ZIPs are generated later by the Provision CAM action.\n'
