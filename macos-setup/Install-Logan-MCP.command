#!/bin/sh
set -eu
umask 077

VM_HOST='130.162.53.112'
VM_PORT='22'
REMOTE_USER='opc'
VM_HOST_PUBLIC_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFcj0yHMayP5k838JNY37ZUoyrv79CYtnkBf0BvXsqz1'
ADMIN_LAUNCH='/opt/logan-mcp/bin/admin-launch'

LOGAN_USER=''
NON_INTERACTIVE=0
SKIP_SSH_TEST=0

usage() {
    printf 'Usage: %s [--user firstname.lastname] [--non-interactive] [--skip-ssh-test]\n' "$0" >&2
    exit 64
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user)
            [ "$#" -ge 2 ] || usage
            LOGAN_USER=$2
            shift 2
            ;;
        --non-interactive)
            NON_INTERACTIVE=1
            shift
            ;;
        --skip-ssh-test)
            SKIP_SSH_TEST=1
            shift
            ;;
        *) usage ;;
    esac
done

if [ -z "$LOGAN_USER" ]; then
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        printf 'A Logan username is required with --user.\n' >&2
        exit 64
    fi
    printf 'Enter Logan username in firstname.lastname format: '
    IFS= read -r LOGAN_USER
fi

LOGAN_USER=$(printf '%s' "$LOGAN_USER" | tr '[:upper:]' '[:lower:]')
if ! printf '%s\n' "$LOGAN_USER" | grep -Eq '^[a-z]+\.[a-z]+$'; then
    printf 'Username must be firstname.lastname using letters only.\n' >&2
    exit 65
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
SOURCE_KEY="$SCRIPT_DIR/logan.key"
INSTALL_DIR="$HOME/.logan-mcp"
KEY_PATH="$INSTALL_DIR/logan.key"
KNOWN_HOSTS_PATH="$INSTALL_DIR/known_hosts"
CODEX_DIR="$HOME/.codex"
CONFIG_PATH="$CODEX_DIR/config.toml"

if [ ! -f "$SOURCE_KEY" ] || [ -L "$SOURCE_KEY" ]; then
    printf 'Missing regular private key beside installer: %s\n' "$SOURCE_KEY" >&2
    exit 66
fi

for tool in awk chmod cp grep mktemp mv sed ssh tr; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Required tool is unavailable: %s\n' "$tool" >&2
        exit 69
    fi
done

mkdir -p "$INSTALL_DIR" "$CODEX_DIR"
chmod 700 "$INSTALL_DIR" "$CODEX_DIR"
for path in "$KEY_PATH" "$KNOWN_HOSTS_PATH" "$CONFIG_PATH"; do
    if [ -L "$path" ] || { [ -e "$path" ] && [ ! -f "$path" ]; }; then
        printf 'Refusing non-regular destination: %s\n' "$path" >&2
        exit 73
    fi
done

CONFIG_CANDIDATE=$(mktemp "$CODEX_DIR/.config.toml.candidate.XXXXXX")
KEY_CANDIDATE=$(mktemp "$INSTALL_DIR/.logan.key.candidate.XXXXXX")
HOST_CANDIDATE=$(mktemp "$INSTALL_DIR/.known-hosts.candidate.XXXXXX")
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    rm -f "$CONFIG_CANDIDATE" "$KEY_CANDIDATE" "$HOST_CANDIDATE"
    exit "$status"
}
trap cleanup EXIT HUP INT TERM
chmod 600 "$CONFIG_CANDIDATE" "$KEY_CANDIDATE" "$HOST_CANDIDATE"

filter_legacy_logan_tables() {
    input=$1
    output=$2
    awk '
        function compact_header(line, value) {
            value = line
            gsub(/[[:space:]\"]/, "", value)
            gsub(/\047/, "", value)
            return value
        }
        function is_target_header(line, value) {
            value = compact_header(line)
            return value ~ /^\[\[?mcp_servers\.(logan-mcp|assurance-logan)(\.|\]|$)/
        }
        {
            if (index($0, "\"\"\"") || index($0, "\047\047\047")) {
                print "Multiline TOML strings are unsupported" > "/dev/stderr"
                exit 42
            }
            trimmed = $0
            sub(/^[[:space:]]*/, "", trimmed)
            if (substr(trimmed, 1, 1) == "[") {
                header = compact_header(trimmed)
                inside_mcp_servers = (header == "[mcp_servers]")
                if (is_target_header(trimmed)) {
                    skip = 1
                    next
                }
                skip = 0
            } else {
                compact = trimmed
                gsub(/[[:space:]\"]/, "", compact)
                gsub(/\047/, "", compact)
                if (!skip && inside_mcp_servers && compact ~ /^(logan-mcp|assurance-logan)=/) {
                    print "Unsupported inline Logan MCP declaration" > "/dev/stderr"
                    exit 44
                }
                if (compact ~ /^mcp_servers\.(logan-mcp|assurance-logan)=/) {
                    print "Unsupported dotted Logan MCP declaration" > "/dev/stderr"
                    exit 43
                }
            }
            if (!skip) print $0
        }
    ' "$input" > "$output"
}

if [ -f "$CONFIG_PATH" ]; then
    filter_legacy_logan_tables "$CONFIG_PATH" "$CONFIG_CANDIDATE" || {
        printf 'Codex config was not changed. Fix the reported TOML structure first.\n' >&2
        exit 65
    }
else
    : > "$CONFIG_CANDIDATE"
fi

if [ -s "$CONFIG_CANDIDATE" ]; then
    printf '\n' >> "$CONFIG_CANDIDATE"
fi

toml_quote() {
    escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
    printf '"%s"' "$escaped"
}

REMOTE_COMMAND="sudo -n $ADMIN_LAUNCH $LOGAN_USER"
{
    printf '[mcp_servers.assurance-logan]\n'
    printf 'command = "ssh"\n'
    printf 'args = ['
    separator=''
    for argument in \
        -T \
        -i "$KEY_PATH" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$KNOWN_HOSTS_PATH" \
        -o ServerAliveInterval=60 \
        -o ServerAliveCountMax=3 \
        -p "$VM_PORT" \
        "$REMOTE_USER@$VM_HOST" \
        "$REMOTE_COMMAND"
    do
        printf '%s' "$separator"
        toml_quote "$argument"
        separator=', '
    done
    printf ']\n'
} >> "$CONFIG_CANDIDATE"

if [ "$(grep -c '^\[mcp_servers\.assurance-logan\]$' "$CONFIG_CANDIDATE")" -ne 1 ]; then
    printf 'Generated Codex configuration failed validation.\n' >&2
    exit 70
fi

cp "$SOURCE_KEY" "$KEY_CANDIDATE"
printf '%s %s\n' "$VM_HOST" "$VM_HOST_PUBLIC_KEY" > "$HOST_CANDIDATE"
chmod 600 "$KEY_CANDIDATE" "$HOST_CANDIDATE" "$CONFIG_CANDIDATE"

if [ "$SKIP_SSH_TEST" -eq 0 ]; then
    ssh -T \
        -i "$KEY_CANDIDATE" \
        -o BatchMode=yes \
        -o IdentitiesOnly=yes \
        -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$HOST_CANDIDATE" \
        -p "$VM_PORT" \
        "$REMOTE_USER@$VM_HOST" \
        "sudo -n test -x $ADMIN_LAUNCH && echo assurance-logan-ok" || {
            printf 'SSH or admin-launch test failed; Codex config was not changed.\n' >&2
            exit 69
        }
fi

if [ -f "$CONFIG_PATH" ]; then
    timestamp=$(date -u '+%Y%m%dT%H%M%SZ')
    cp "$CONFIG_PATH" "$CONFIG_PATH.backup-$timestamp"
    chmod 600 "$CONFIG_PATH.backup-$timestamp"
fi

mv -f "$KEY_CANDIDATE" "$KEY_PATH"
mv -f "$HOST_CANDIDATE" "$KNOWN_HOSTS_PATH"
mv -f "$CONFIG_CANDIDATE" "$CONFIG_PATH"
chmod 600 "$KEY_PATH" "$KNOWN_HOSTS_PATH" "$CONFIG_PATH"

printf 'Configured Codex MCP server: assurance-logan\n'
printf 'User identity: %s\n' "$LOGAN_USER"
printf 'Restart Codex completely before using the connection.\n'
if [ "$NON_INTERACTIVE" -eq 0 ]; then
    printf 'Press Return to close this window: '
    IFS= read -r _answer || true
fi
